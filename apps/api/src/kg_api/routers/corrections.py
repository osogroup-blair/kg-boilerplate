"""Corrections (phase 8, blueprint Flow D, AT-13, ADM-03): fix a fact, an entity or a memory without rewriting history, after seeing what it would touch.

Every correction is an audited ledger command (`correction.*`, `entity.split`) that needs a closed reason code, an Idempotency-Key and the target's revision (If-Match).
The target is read through the caller's subject filter: what you cannot see you cannot correct, and a hidden, frozen or missing id is one 404. Postgres changes first
and the graph follows; the next context build re-materialises from the new state (there is no context cache to invalidate)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_db.session import tenant_session
from kg_governance.audit import AuditWriter
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_governance.grants import ScopeGrants
from kg_knowledge.commands import CommandContext
from kg_knowledge.correction_models import (
    CorrectionOut,
    CorrectionRecord,
    EntityCorrectionIn,
    FactCorrectionIn,
    MemoryCorrectionIn,
    PreviewOut,
)
from kg_memory.reads import Viewer
from kg_memory.writers import WriteContext
from kg_obs import context

from kg_api.commands_runner import project_now
from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1", tags=["corrections"])
NO_STORE = {"Cache-Control": "no-store"}
RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": CorrectionOut},
    404: {"description": "not found, or hidden from the caller"},
    409: {"description": "invalid_correction / idempotency_mismatch / invalid_transition"},
    412: {"description": "If-Match does not match the target's revision"},
    422: {"description": "rule_violation / validation_failed / unknown_entity"},
    428: {"description": "If-Match is required"},
}
FactPreviewKind = Literal["reject", "supersede", "end", "restore"]
EntityPreviewKind = Literal["correct_attribute", "rename", "split"]
MemoryPreviewKind = Literal["supersede", "reject", "expire", "dispute"]


def _done(out: CorrectionOut, replayed: bool) -> JSONResponse:
    headers = {**NO_STORE, "ETag": etag(out.revision), "Location": out.status_url}
    if replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(out.model_dump(mode="json"), headers=headers)


@router.post("/relationships/{fact_id}/corrections", response_model=CorrectionOut, responses=RESPONSES)
@requires(Action.CORRECTION_APPLY)
async def correct_relationship(
    fact_id: uuid.UUID,
    body: FactCorrectionIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """Reject, supersede, end or restore a fact. A fact stays in the ledger either way; what changes is whether the platform still holds it to be true."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    async with tenant_session(state.engine, ws, principal.principal_id) as conn:
        audit = AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
        grants = await ScopeGrants.for_principal(conn, principal)
        subjects = await state.knowledge.reads.subject_filter(conn, ws.workspace_id, grants)
        applied = await state.knowledge.corrections.fact(CommandContext(conn, ws, principal, audit), key, fact_id, body, revision, subjects)
    if not applied.replayed:
        applied.out.projection_status = await project_now(state, ws, applied.out.command_id)  # type: ignore[assignment]
    return _done(applied.out, applied.replayed)


@router.post("/entities/{entity_id}/corrections", response_model=CorrectionOut, responses=RESPONSES)
@requires(Action.CORRECTION_APPLY)
async def correct_entity(
    entity_id: uuid.UUID,
    body: EntityCorrectionIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """Correct an attribute, rename (the old name stays an alias; the id never changes) or SPLIT an identity: the compensating command for a wrong merge. A split
    moves the mentions, facts, memories and strong identifiers you name to a new entity that starts with no grants."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    async with tenant_session(state.engine, ws, principal.principal_id) as conn:
        audit = AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
        grants = await ScopeGrants.for_principal(conn, principal)
        subjects = await state.knowledge.reads.subject_filter(conn, ws.workspace_id, grants)
        applied = await state.knowledge.corrections.entity(
            CommandContext(conn, ws, principal, audit), key, entity_id, body, revision, subjects
        )
    if not applied.replayed:
        applied.out.projection_status = await project_now(state, ws, applied.out.command_id)  # type: ignore[assignment]
    return _done(applied.out, applied.replayed)


@router.post("/memories/{memory_id}/corrections", response_model=CorrectionOut, responses=RESPONSES)
@requires(Action.MEMORY_CURATE)
async def correct_memory(
    memory_id: uuid.UUID,
    body: MemoryCorrectionIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """Supersede a memory with corrected wording, or reject, expire or dispute it. The replacement is embedded; the old row leaves search and context at once."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    grants = await ScopeGrants.for_principal(db, principal)
    sf = await state.knowledge.reads.subject_filter(db, principal.workspace_id, grants)
    audit = AuditWriter(db, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
    w = WriteContext(db, ws, principal, audit, grants, curator=True, subjects=sf)
    out, replayed = await state.memory.corrections.apply(w, memory_id, body, key, revision)
    return _done(out, replayed)


@router.get("/relationships/{fact_id}/corrections/preview", response_model=PreviewOut)
@requires(Action.CORRECTION_APPLY)
async def preview_relationship_correction(
    fact_id: uuid.UUID, kind: FactPreviewKind, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep
) -> JSONResponse:
    grants = await ScopeGrants.for_principal(db, principal)
    sf = await state.knowledge.reads.subject_filter(db, ws.workspace_id, grants)
    out = await state.knowledge.corrections.preview_fact(
        db, ws, principal, fact_id, kind, sf, include_contexts=is_allowed(principal, Action.WORKSPACE_ADMIN)
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/entities/{entity_id}/corrections/preview", response_model=PreviewOut)
@requires(Action.CORRECTION_APPLY)
async def preview_entity_correction(
    entity_id: uuid.UUID, kind: EntityPreviewKind, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep
) -> JSONResponse:
    grants = await ScopeGrants.for_principal(db, principal)
    sf = await state.knowledge.reads.subject_filter(db, ws.workspace_id, grants)
    out = await state.knowledge.corrections.preview_entity(
        db, ws, principal, entity_id, kind, sf, include_contexts=is_allowed(principal, Action.WORKSPACE_ADMIN)
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/memories/{memory_id}/corrections/preview", response_model=PreviewOut)
@requires(Action.MEMORY_CURATE)
async def preview_memory_correction(
    memory_id: uuid.UUID, kind: MemoryPreviewKind, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep
) -> JSONResponse:
    grants = await ScopeGrants.for_principal(db, principal)
    sf = await state.knowledge.reads.subject_filter(db, ws.workspace_id, grants)
    v = Viewer(principal, grants, curator=True, subjects=sf)
    out = await state.memory.corrections.preview(db, ws, v, memory_id, kind, include_contexts=is_allowed(principal, Action.WORKSPACE_ADMIN))
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/corrections", response_model=list[CorrectionRecord])
@requires(Action.CORRECTION_READ)
async def list_corrections(
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    target_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    """The history of corrections: ids, kinds and reason codes for everyone with the action; the reason TEXT only for reviewers and admins."""
    staff = is_allowed(principal, Action.CORRECTION_APPLY)
    rows = await state.knowledge.corrections.history(db, ws, target_id=target_id, include_text=staff, limit=limit)
    return JSONResponse([r.model_dump(mode="json") for r in rows], headers=NO_STORE)
