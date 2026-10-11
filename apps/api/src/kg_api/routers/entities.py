"""Entities: the directory, detail with history, and the deterministic create/update commands (Flow B)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_knowledge.models import CommandOut, EntityCreateIn, EntityDetailOut, EntityOut, EntityUpdateIn, FactOut, Instant

from kg_api.commands_runner import run_command
from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1/entities", tags=["entities"])
LimitQ = Annotated[int, Query(ge=1, le=100)]
COMMAND_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": CommandOut, "description": "matched_existing: an entity with that external identifier already exists"},
    201: {"model": CommandOut, "description": "Applied and projected"},
    202: {"model": CommandOut, "description": "Accepted: projection pending, or queued for review (needs_review)"},
    409: {"description": "no_active_ontology / idempotency_mismatch"},
    422: {"description": "rule_violation (details.violations) / unknown_entity / validation_failed"},
}


@router.post("", response_model=None, status_code=201, responses=COMMAND_RESPONSES)
@requires(Action.ENTITY_WRITE)
async def create_entity(
    body: EntityCreateIn, request: Request, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    key = require_key(idempotency_key)
    return await run_command(request, state, principal, lambda cc: state.knowledge.pipeline.entity_create(cc, key, body))


@router.get("", response_model=Page[EntityOut])
@requires(Action.ENTITY_READ)
async def list_entities(
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    type: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Literal["active", "deleted"] | None = None,
    limit: LimitQ = 50,
    cursor: str | None = None,
) -> Page[EntityOut]:
    return await state.knowledge.reads.list_entities(
        db,
        ws,
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
        type_key=type,
        q=q,
        status=status,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{entity_id}", response_model=EntityDetailOut)
@requires(Action.ENTITY_READ)
async def get_entity(
    entity_id: uuid.UUID,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    include: Annotated[Literal["history"] | None, Query()] = None,
    as_of: Annotated[Instant | None, Query()] = None,
    known_at: Annotated[Instant | None, Query()] = None,
) -> JSONResponse:
    out = await state.knowledge.reads.get_entity(
        db,
        ws,
        entity_id,
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
        include_history=include == "history",
        as_of=as_of,
        known_at=known_at,
    )
    return JSONResponse(out.model_dump(mode="json"), headers={"ETag": etag(out.revision)})


@router.patch(
    "/{entity_id}",
    response_model=None,
    responses={**COMMAND_RESPONSES, 412: {"description": "If-Match does not match"}, 428: {"description": "If-Match required"}},
)
@requires(Action.ENTITY_WRITE)
async def update_entity(
    entity_id: uuid.UUID,
    body: EntityUpdateIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)
    revision = parse_if_match(if_match)
    return await run_command(
        request, state, principal, lambda cc: state.knowledge.pipeline.entity_update(cc, key, entity_id, body, revision)
    )


@router.get("/{entity_id}/relationships", response_model=list[FactOut])
@requires(Action.ENTITY_READ)
async def entity_relationships(
    entity_id: uuid.UUID,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    direction: Literal["out", "in", "both"] = "both",
    type: Annotated[str | None, Query(max_length=64)] = None,
    as_of: Annotated[Instant | None, Query()] = None,
    known_at: Annotated[Instant | None, Query()] = None,
    include_history: bool = False,
) -> list[FactOut]:
    await state.knowledge.reads.entity_exists(db, ws, entity_id)  # a foreign or unknown id is a 404, never an empty list
    return await state.knowledge.reads.list_facts(
        db,
        ws,
        entity_id=entity_id,
        direction=direction,
        rel_type=type,
        as_of=as_of,
        known_at=known_at,
        include_history=include_history,
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
    )
