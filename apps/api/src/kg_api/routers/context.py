"""The context engine (phase 6): build, re-materialise, trace, debug snapshot, listing, profiles, and a memory's usage in contexts.

`POST /v1/context/build` is the product. The Context Lab calls the SAME endpoint with `X-Act-As-Principal` (workspace admins only, agents and apps of the same workspace
only, audited): there is no second code path. Context responses are never cached. A build, a trace or a debug snapshot that the caller may not see is a 404 identical to
an id that never existed."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from kg_common.errors import Forbidden, NotFound
from kg_context.actas import resolve_target
from kg_context.models import (
    ContextBuildIn,
    ContextOut,
    ContextSummary,
    DebugOut,
    MemoryUsageOut,
    TraceItemOut,
    TraceOut,
)
from kg_db.session import tenant_session
from kg_domain.enums import Role
from kg_domain.envelope import Page
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants
from kg_obs import context as obs_context

from kg_api.deps import DbDep, PrincipalDep, StateDep

router = APIRouter(prefix="/v1/context", tags=["context"])
usage_router = APIRouter(prefix="/v1/memories", tags=["context"])
NO_STORE = {"Cache-Control": "no-store"}
ActAsHeader = Annotated[uuid.UUID | None, Header(alias="X-Act-As-Principal")]


@router.post(
    "/build",
    response_model=ContextOut,
    responses={429: {"description": "rate_limited"}, 503: {"description": "context_incomplete / feature_disabled"}},
)
@requires(Action.CONTEXT_BUILD)
async def build_context(body: ContextBuildIn, state: StateDep, principal: PrincipalDep, act_as: ActAsHeader = None) -> JSONResponse:
    """The smallest sufficiently informative, authorised, budgeted, cited context for a task (blueprint 02-domain/05). The workspace and the identity come from the
    credential; a body that names them is a 422. With `X-Act-As-Principal` an admin builds as an agent or app of the workspace (Context Lab, audited)."""
    caller: Principal = principal
    effective, acting = principal, None
    if act_as is not None and act_as != principal.principal_id:
        if not is_allowed(principal, Action.CONTEXT_LAB):
            raise Forbidden("Action not permitted", details={"action": Action.CONTEXT_LAB.value})
        effective = await resolve_target(state.engine, principal, act_as)
        if not is_allowed(effective, Action.CONTEXT_BUILD):
            raise Forbidden("That principal cannot build contexts", details={"reason": "not_actable"})
        acting = principal
    if body.debug_snapshot and not is_allowed(caller, Action.CONTEXT_DEBUG):
        raise Forbidden("Action not permitted", details={"action": Action.CONTEXT_DEBUG.value})
    ctx = state.context
    if acting is None:  # a Lab build is the admin looking through the agent's permissions, not the agent using its quota
        async with tenant_session(state.engine, WorkspaceContext.of(effective), effective.principal_id) as qconn:
            await state.quotas.admit_context(qconn, effective)
    async with ctx.limiter.aadmit(caller.principal_id, caller.workspace_id):
        out = await ctx.service.build(effective, body, act_as_by=acting)
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/profiles")
@requires(Action.CONTEXT_BUILD)
async def list_profiles(state: StateDep, principal: PrincipalDep) -> JSONResponse:
    """The effective profiles for this workspace (built-ins overridden by the active ontology): name, caps, sections, modes."""
    return JSONResponse(await state.context.service.profiles(principal), headers=NO_STORE)


@router.get("", response_model=Page[ContextSummary])
@requires(Action.CONTEXT_TRACE)
async def list_contexts(
    state: StateDep, principal: PrincipalDep, db: DbDep, limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = None
) -> JSONResponse:
    """A workspace admin sees every build's metadata; anyone else sees only their own."""
    own = None if Role.WORKSPACE_ADMIN in principal.roles else principal.principal_id
    page = await state.context.reads.list(db, principal.workspace_id, principal_id=own, limit=limit, cursor=cursor)
    return JSONResponse(page.model_dump(mode="json"), headers=NO_STORE)


@router.get("/{context_id}", response_model=ContextOut)
@requires(Action.CONTEXT_READ)
async def get_context(context_id: uuid.UUID, state: StateDep, principal: PrincipalDep) -> JSONResponse:
    """The package re-materialised under YOUR current authorisation: anything no longer visible or active is omitted and counted in `withdrawn`."""
    out = await state.context.service.rematerialize(principal, context_id)
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/{context_id}/trace", response_model=TraceOut)
@requires(Action.CONTEXT_TRACE)
async def get_trace(context_id: uuid.UUID, state: StateDep, principal: PrincipalDep, db: DbDep) -> JSONResponse:
    """Why a build chose what it chose: the plan, versions, per-source counts, every authorised candidate with its score, features, tier and exclusion reason, the
    token accounting, watermarks and latencies. Metadata only (ids and numbers): the owner of the build and workspace admins only."""
    reads = state.context.reads
    trace = await reads.get(db, principal.workspace_id, context_id)
    if not state.context.service.can_see_trace(principal, trace):
        raise NotFound()
    items = await reads.items(db, principal.workspace_id, context_id, hide_frozen=True)
    out = TraceOut(
        context_id=trace.id,
        principal_id=trace.principal_id,
        act_as_by=trace.act_as_by,
        profile=trace.profile,
        profile_source=trace.profile_source,
        ontology_version_id=trace.ontology_version_id,
        status=trace.status,
        compression=trace.compression,
        task_tokens=trace.task_tokens,
        request=trace.request,
        plan=trace.plan,
        versions=trace.versions,
        counts=trace.counts,
        unavailable=trace.unavailable,
        watermarks=trace.watermarks,
        latencies=trace.latencies,
        max_tokens=trace.max_tokens,
        used_tokens=trace.used_tokens,
        required_tokens=trace.required_tokens,
        created_at=trace.created_at,
        items=[
            TraceItemOut(
                position=i.position,
                handle=i.handle,
                kind=i.item_kind,
                id=i.item_id,
                section=i.section,
                tier=i.tier,
                score=float(i.score),
                features=dict(i.features),
                tokens=i.tokens,
                included=i.included,
                exclusion_reason=i.exclusion_reason,
                duplicate_of=i.duplicate_of,
            )
            for i in items
        ],
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.get("/{context_id}/debug", response_model=DebugOut)
@requires(Action.CONTEXT_DEBUG)
async def get_debug_snapshot(context_id: uuid.UUID, state: StateDep, principal: PrincipalDep, db: DbDep) -> JSONResponse:
    """The opt-in encrypted snapshot (the task and the rendered text), until it expires. Admin only, audited, never cached."""
    snap = await state.context.store.read_debug(db, principal.workspace_id, context_id)
    await AuditWriter(
        db, principal, request_id=obs_context.request_id_var.get(), correlation_id=obs_context.correlation_id_var.get()
    ).record("context.debug_read", resource_type="context", resource_id=context_id, metadata=AuditMetadata(context_id=context_id))
    out = DebugOut(
        context_id=context_id, expires_at=snap["expires_at"], task=snap["task"], query=snap["query"], context_text=snap["context_text"]
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@usage_router.get("/{memory_id}/usage", response_model=MemoryUsageOut)
@requires(Action.MEMORY_READ_PRIVATE)
async def memory_usage(memory_id: uuid.UUID, state: StateDep, principal: PrincipalDep, db: DbDep) -> JSONResponse:
    """How many context builds included this memory and when last (admin only: it reveals what agents have been reading)."""
    grants = await ScopeGrants.for_principal(db, principal, may_reveal_private=True)
    await state.memory.reads.visible_row(
        db, Viewer(principal, grants, curator=True, reveal_private=True), memory_id
    )  # a memory that does not exist is a 404
    out = MemoryUsageOut(**await state.context.reads.usage(db, principal.workspace_id, "memory", str(memory_id)))
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)
