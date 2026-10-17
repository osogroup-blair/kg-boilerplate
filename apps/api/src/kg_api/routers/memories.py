"""Curated memory (phase 5): explicit save, proposals, scoped search, the Explorer's list and detail, and the lifecycle operations.

Every read is scoped by the caller's grants in SQL (kg_memory.scopes); a memory the caller cannot see is a 404 identical to an id that never existed. Memory
responses are never cached. Writes take an Idempotency-Key; mutations of an existing memory also take If-Match (the memory's revision)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_common.errors import Forbidden
from kg_domain.envelope import Page
from kg_domain.principal import Principal
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_memory.models import (
    Classification,
    GrantOut,
    MemoryOut,
    MemoryPublishIn,
    MemorySaveIn,
    MemoryStatus,
    MemorySummary,
    MemoryType,
    Origin,
    ProposalIn,
    ReasonIn,
    SearchIn,
    SearchOut,
    SupersedeIn,
    WriteOut,
)
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants
from kg_memory.writers import WriteContext, Written
from kg_obs import context
from sqlalchemy import text

from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1/memories", tags=["memories"])
scopes_router = APIRouter(prefix="/v1/scopes", tags=["memories"])
NO_STORE = {"Cache-Control": "no-store"}
LimitQ = Annotated[int, Query(ge=1, le=100)]
WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": WriteOut, "description": "deduplicated: an identical live memory exists and now cites the new evidence"},
    201: {"model": WriteOut, "description": "Saved and active"},
    202: {"model": WriteOut, "description": "Proposed: waiting for review (or accepted by policy, see status)"},
    403: {"description": "forbidden / use_proposal"},
    409: {"description": "idempotency_mismatch / invalid_transition / duplicate_memory"},
    412: {"description": "If-Match does not match the memory's revision"},
    422: {
        "description": "invalid_scope / invalid_evidence / attribution_required / content_too_long / working_must_be_private / unknown_entity"
    },
    428: {"description": "If-Match is required"},
}


async def _viewer(db: Any, state: Any, principal: Principal, *, reveal_private: bool = False) -> Viewer:
    may_reveal = is_allowed(principal, Action.MEMORY_READ_PRIVATE)
    if reveal_private and not may_reveal:
        raise Forbidden("Action not permitted", details={"action": Action.MEMORY_READ_PRIVATE.value})
    grants = await ScopeGrants.for_principal(db, principal, may_reveal_private=may_reveal)
    sf = await state.knowledge.reads.subject_filter(db, principal.workspace_id, grants)
    return Viewer(principal, grants, curator=is_allowed(principal, Action.MEMORY_CURATE), reveal_private=reveal_private, subjects=sf)


async def _write_context(db: Any, request: Request, state: Any, principal: Principal, ws: Any) -> WriteContext:
    grants = await ScopeGrants.for_principal(db, principal)
    sf = await state.knowledge.reads.subject_filter(db, principal.workspace_id, grants)
    audit = AuditWriter(db, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
    return WriteContext(db, ws, principal, audit, grants, curator=is_allowed(principal, Action.MEMORY_CURATE), subjects=sf)


def _respond(written: Written) -> JSONResponse:
    headers = {**NO_STORE, "ETag": etag(written.out.revision)}
    if written.replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(written.out.model_dump(mode="json"), status_code=written.http_status, headers=headers)


@router.post("", response_model=WriteOut, status_code=201, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_WRITE_OWN)
async def save_memory(
    body: MemorySaveIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """An explicit save (a trusted app or admin: needs a source and a purpose for shared scopes) or a working / agent-private note. A shared save by an agent is a
    403 (`use_proposal`): agents propose."""
    key = require_key(idempotency_key)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.save(w, body, key))


@router.post("/proposals", response_model=WriteOut, status_code=202, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_PROPOSE)
async def propose_memory(
    body: ProposalIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """A candidate with evidence the caller can itself see. The platform decides the status (policy); `requested_scope` is advisory and checked against the
    caller's grants. An agent can never activate a procedural memory."""
    key = require_key(idempotency_key)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.propose(w, body, key))


@router.post("/search", response_model=SearchOut)
@requires(Action.MEMORY_READ)
async def search_memories(body: SearchIn, state: StateDep, principal: PrincipalDep, db: DbDep) -> JSONResponse:
    """Hybrid (lexical + vector) search whose authorisation is part of the ranking query. Every principal gets results filtered by its OWN grants."""
    v = await _viewer(db, state, principal)
    out = await state.memory.search.run(db, v, body)
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("", response_model=Page[MemorySummary])
@requires(Action.MEMORY_READ)
async def list_memories(
    state: StateDep,
    principal: PrincipalDep,
    db: DbDep,
    request: Request,
    type: MemoryType | None = None,
    status: MemoryStatus | None = None,
    subject_id: uuid.UUID | None = None,
    project: Annotated[str | None, Query(max_length=64)] = None,
    origin: Origin | None = None,
    classification: Classification | None = None,
    created_by: uuid.UUID | None = None,
    updated_since: dt.datetime | None = None,
    reveal_private: bool = False,
    limit: LimitQ = 50,
    cursor: str | None = None,
) -> JSONResponse:
    v = await _viewer(db, state, principal, reveal_private=reveal_private)
    page = await state.memory.reads.list(
        db,
        v,
        type_=type,
        status=status,
        subject_id=subject_id,
        project=project,
        origin=origin,
        classification=classification,
        created_by=created_by,
        updated_since=updated_since,
        limit=limit,
        cursor=cursor,
    )
    if reveal_private:  # audited: counts only, in the same transaction as the read
        await AuditWriter(db, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()).record(
            "memory.private_read", resource_type="memory", metadata=AuditMetadata(count_memories=len(page.items))
        )
    return JSONResponse(page.model_dump(mode="json"), headers=NO_STORE)


@router.get("/{memory_id}", response_model=MemoryOut)
@requires(Action.MEMORY_READ)
async def get_memory(
    memory_id: uuid.UUID, state: StateDep, principal: PrincipalDep, db: DbDep, reveal_private: bool = False
) -> JSONResponse:
    """A memory the caller cannot see is a 404, the same as an id that never existed. `reveal_private` (admins, audited) opens another principal's private
    memory; the audit entry commits with the read."""
    v = await _viewer(db, state, principal, reveal_private=reveal_private)
    out = await state.memory.reads.get(db, v, memory_id)
    private = reveal_private and any(s.kind in ("agent", "session") and not v.grants.can_use(s.kind, s.ref) for s in out.scopes)
    if private:
        await AuditWriter(db, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()).record(
            "memory.private_read",
            resource_type="memory",
            resource_id=memory_id,
            metadata=AuditMetadata(memory_id=memory_id, memory_type=out.type, scope_kind="agent"),
        )
    return JSONResponse(out.model_dump(mode="json"), headers={**NO_STORE, "ETag": etag(out.revision)})


@router.post("/{memory_id}/supersede", response_model=WriteOut, status_code=201, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_READ)
async def supersede_memory(
    memory_id: uuid.UUID,
    body: SupersedeIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """A new active memory replaces this one; the old one is kept as `superseded` with its evidence and a `replaces` lineage row (MEM-04). Shared memory needs
    memory.curate; private memory belongs to its owner."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.supersede(w, memory_id, body, key, revision))


@router.post("/{memory_id}/expire", response_model=WriteOut, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_READ)
async def expire_memory(
    memory_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.expire(w, memory_id, body, key, revision))


@router.post("/{memory_id}/dispute", response_model=WriteOut, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_CURATE)
async def dispute_memory(
    memory_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """Marks a live memory disputed and opens a review item (`memory_conflict`) that decides how the dispute ends."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.dispute(w, memory_id, body, key, revision))


@router.post("/{memory_id}/publish", response_model=WriteOut, status_code=202, responses=WRITE_RESPONSES)
@requires(Action.MEMORY_WRITE_OWN)
async def publish_memory(
    memory_id: uuid.UUID,
    body: MemoryPublishIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
    as_type: Annotated[
        MemoryType | None, Query(description="the type of the published memory (a working note becomes semantic by default)")
    ] = None,
) -> JSONResponse:
    """The owner shares a private memory: a NEW proposal with `published_from` lineage. The original stays private (MEM-02)."""
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    w = await _write_context(db, request, state, principal, ws)
    return _respond(await state.memory.writer.publish(w, memory_id, body, key, revision, as_type=as_type))


@scopes_router.get("", response_model=list[GrantOut], deprecated=True)
@requires(Action.PRINCIPAL_READ)
async def list_scope_grants(ws: WorkspaceDep, db: DbDep, principal_id: uuid.UUID | None = None) -> list[GrantOut]:
    """DEPRECATED alias: use GET /v1/principals/{id}/grants (P7), which also carries privileges. Kept for compatibility with P5 clients."""
    rows = (
        await db.execute(
            text(
                "SELECT principal_id, scope_kind, scope_ref, created_at FROM principal_scopes WHERE workspace_id = :ws "
                "AND (CAST(:p AS uuid) IS NULL OR principal_id = :p) ORDER BY principal_id, scope_kind, scope_ref"
            ),
            {"ws": ws.workspace_id, "p": principal_id},
        )
    ).all()
    return [GrantOut(principal_id=r.principal_id, scope_kind=r.scope_kind, scope_ref=r.scope_ref, created_at=r.created_at) for r in rows]
