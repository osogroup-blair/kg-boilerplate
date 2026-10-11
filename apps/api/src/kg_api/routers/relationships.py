"""Relationships (facts): assert, end, and read with validity, record time and provenance (KGR-03/04/05)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_knowledge.models import FactOut, RelationshipAssertIn, RelationshipEndIn

from kg_api.commands_runner import run_command
from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key
from kg_api.routers.entities import COMMAND_RESPONSES

router = APIRouter(prefix="/v1/relationships", tags=["relationships"])


@router.post("", response_model=None, status_code=201, responses=COMMAND_RESPONSES)
@requires(Action.ENTITY_WRITE)
async def assert_relationship(
    body: RelationshipAssertIn, request: Request, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    key = require_key(idempotency_key)
    return await run_command(request, state, principal, lambda cc: state.knowledge.pipeline.relationship_assert(cc, key, body))


@router.post("/{fact_id}/end", response_model=None, responses=COMMAND_RESPONSES)
@requires(Action.ENTITY_WRITE)
async def end_relationship(
    fact_id: uuid.UUID,
    body: RelationshipEndIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)
    return await run_command(request, state, principal, lambda cc: state.knowledge.pipeline.relationship_end(cc, key, fact_id, body))


@router.get("/{fact_id}", response_model=FactOut)
@requires(Action.ENTITY_READ)
async def get_relationship(fact_id: uuid.UUID, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep) -> FactOut:
    return await state.knowledge.reads.get_fact(db, ws, fact_id, can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE))
