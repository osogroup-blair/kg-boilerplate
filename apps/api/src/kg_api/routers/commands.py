"""The command ledger: every command, its decision trace and its projection status (the status URL of a 202)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, requires
from kg_knowledge.models import CommandDetailOut, CommandStatus, CommandSummary, CommandTypeName

from kg_api.deps import DbDep, StateDep, WorkspaceDep

router = APIRouter(prefix="/v1/commands", tags=["commands"])


@router.get("", response_model=Page[CommandSummary])
@requires(Action.COMMAND_READ)
async def list_commands(
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    status: CommandStatus | None = None,
    type: CommandTypeName | None = None,
    entity_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[CommandSummary]:
    return await state.knowledge.reads.list_commands(
        db, ws, status=status, command_type=type, entity_id=entity_id, limit=limit, cursor=cursor
    )


@router.get("/{command_id}", response_model=CommandDetailOut)
@requires(Action.COMMAND_READ)
async def get_command(command_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep) -> CommandDetailOut:
    return await state.knowledge.reads.get_command(db, ws, command_id)
